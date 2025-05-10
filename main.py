import os
import json
import datetime
import requests
import functions_framework
from datetime import timezone
from google.cloud import bigquery
from google.cloud import monitoring_v3


@functions_framework.http
def send_billing_report(request):
    """매일 아침 GCP 사용량과 비용을 Discord로 전송"""

    # Discord Webhook URL (환경변수로 설정)
    DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')
    PROJECT_ID = os.environ.get('GCP_PROJECT_ID')
    BILLING_ACCOUNT_ID = os.environ.get('BILLING_ACCOUNT_ID')

    if not all([DISCORD_WEBHOOK_URL, PROJECT_ID, BILLING_ACCOUNT_ID]):
        return 'Missing required environment variables', 400

    # 현재 날짜와 어제 날짜 계산
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)

    try:
        # 1. 일일 비용 데이터 조회
        billing_data = get_daily_cost(PROJECT_ID, BILLING_ACCOUNT_ID, yesterday)

        # 2. 리소스 사용량 데이터 조회
        resource_usage = get_resource_usage(PROJECT_ID, yesterday)

        # 3. Discord 메시지 생성
        message = create_discord_message(billing_data, resource_usage, yesterday)

        # 4. Discord로 전송
        send_to_discord(DISCORD_WEBHOOK_URL, message)

        return 'Report sent successfully', 200
    except Exception as e:
        print(f"Error in send_billing_report: {e}")
        return f'Error: {str(e)}', 500


def get_daily_cost(project_id, billing_account_id, date):
    """BigQuery에서 일일 비용 조회"""
    client = bigquery.Client()

    # BigQuery에서 billing export 데이터 조회
    query = f"""
    SELECT
        service.description as service_name,
        sku.description as sku_description,
        SUM(cost) as total_cost
    FROM `{project_id}.billing_export.gcp_billing_export*`
    WHERE 
        billing_account_id = @billing_account_id
        AND DATE(usage_start_time) = @target_date
    GROUP BY service_name, sku_description
    ORDER BY total_cost DESC
    LIMIT 10
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("billing_account_id", "STRING", billing_account_id),
            bigquery.ScalarQueryParameter("target_date", "DATE", date.strftime("%Y-%m-%d")),
        ]
    )

    try:
        query_job = client.query(query, job_config=job_config)
        results = query_job.result()

        billing_data = []
        total_cost = 0

        for row in results:
            billing_data.append({
                'service': row.service_name,
                'sku': row.sku_description,
                'cost': row.total_cost
            })
            total_cost += row.total_cost

        return {
            'items': billing_data,
            'total_cost': total_cost
        }
    except Exception as e:
        print(f"Error in get_daily_cost: {e}")
        return {
            'items': [],
            'total_cost': 0
        }


def get_resource_usage(project_id, date):
    """Monitoring API에서 리소스 사용량 조회"""
    try:
        client = monitoring_v3.MetricServiceClient()
        project_name = f"projects/{project_id}"

        # 시간 간격 설정 (어제 하루)
        interval = monitoring_v3.TimeInterval(
            {
                "end_time": datetime.datetime.combine(date + datetime.timedelta(days=1), datetime.time.min,
                                                      tzinfo=timezone.utc),
                "start_time": datetime.datetime.combine(date, datetime.time.min, tzinfo=timezone.utc),
            }
        )

        # Compute Engine CPU 사용률 조회
        cpu_usage = get_metric_data(client, project_name,
                                    'compute.googleapis.com/instance/cpu/utilization',
                                    interval)

        # Cloud Storage 사용량 조회
        storage_usage = get_metric_data(client, project_name,
                                        'storage.googleapis.com/storage/total_bytes',
                                        interval)

        # Cloud SQL 메모리 사용률 조회
        sql_memory = get_metric_data(client, project_name,
                                     'cloudsql.googleapis.com/database/memory/utilization',
                                     interval)

        return {
            'cpu_usage': cpu_usage,
            'storage_usage': storage_usage,
            'sql_memory': sql_memory
        }
    except Exception as e:
        print(f"Error in get_resource_usage: {e}")
        return {
            'cpu_usage': None,
            'storage_usage': None,
            'sql_memory': None
        }


def get_metric_data(client, project_name, metric_type, interval):
    """특정 메트릭의 데이터 조회"""
    try:
        results = client.list_time_series(
            request={
                "name": project_name,
                "filter": f'metric.type="{metric_type}"',
                "interval": interval,
                "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            }
        )

        values = []
        for ts in results:
            for point in ts.points:
                values.append(point.value.double_value or point.value.int64_value)

        if values:
            return {
                'average': sum(values) / len(values),
                'maximum': max(values),
                'minimum': min(values)
            }
        return None
    except Exception as e:
        print(f"Error getting metric {metric_type}: {e}")
        return None


def create_discord_message(billing_data, resource_usage, date):
    """Discord 메시지 형식 생성"""
    # Embed 메시지 생성
    embed = {
        "title": f"📊 GCP 일일 리포트 - {date.strftime('%Y-%m-%d')}",
        "color": 0x4285F4,  # Google Blue
        "fields": [],
        "footer": {
            "text": "GCP 비용 모니터링 시스템"
        },
        "timestamp": datetime.datetime.now(timezone.utc).isoformat()
    }

    # 총 비용 추가
    embed["fields"].append({
        "name": "💰 총 비용",
        "value": f"${billing_data['total_cost']:.2f} USD",
        "inline": False
    })

    # 상위 비용 서비스 추가
    if billing_data['items']:
        top_costs = "\n".join([
            f"• {item['service']}: ${item['cost']:.2f}"
            for item in billing_data['items'][:5]
        ])
        embed["fields"].append({
            "name": "📋 상위 비용 서비스",
            "value": top_costs,
            "inline": False
        })

    # 리소스 사용량 추가
    if resource_usage['cpu_usage']:
        embed["fields"].append({
            "name": "🖥️ CPU 사용률",
            "value": f"평균: {resource_usage['cpu_usage']['average']:.1f}%\n최대: {resource_usage['cpu_usage']['maximum']:.1f}%",
            "inline": True
        })

    if resource_usage['storage_usage']:
        storage_gb = resource_usage['storage_usage']['average'] / (1024 ** 3)
        embed["fields"].append({
            "name": "💾 Storage 사용량",
            "value": f"{storage_gb:.2f} GB",
            "inline": True
        })

    if resource_usage['sql_memory']:
        embed["fields"].append({
            "name": "🗄️ Cloud SQL 메모리",
            "value": f"평균: {resource_usage['sql_memory']['average']:.1f}%",
            "inline": True
        })

    return {"embeds": [embed]}


def send_to_discord(webhook_url, message):
    """Discord로 메시지 전송"""
    headers = {'Content-Type': 'application/json'}
    response = requests.post(webhook_url, json=message, headers=headers)

    if response.status_code != 204:
        raise Exception(f"Discord webhook failed: {response.status_code}, {response.text}")

    return response