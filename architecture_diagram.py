from diagrams import Diagram, Cluster, Edge
from diagrams.onprem.client import User, Users
from diagrams.onprem.compute import Server
from diagrams.onprem.database import PostgreSQL
from diagrams.onprem.ml import Kubeflow
from diagrams.onprem.network import Internet
from diagrams.programming.framework import Flask
from diagrams.saas.chat import Slack
from diagrams.aws.compute import Lambda
from diagrams.aws.storage import S3
from diagrams.aws.ml import Rekognition
from diagrams.azure.analytics import Databricks
from diagrams.gcp.analytics import BigQuery
from diagrams.custom import Custom

# 다이어그램 생성
with Diagram("pGluc-Webex 아키텍처", show=False, direction="TB", filename="pgluc_webex_architecture"):
    
    # 외부 연결
    internet = Internet("인터넷")
    
    # 환자 측 구성요소
    with Cluster("환자 측"):
        patient = User("환자")
        mobile_app = Custom("모바일 앱", "./icons/mobile-app.png")
        cgm_device = Custom("CGM 장치", "./icons/cgm-device.png")
        wearable = Custom("웨어러블 기기", "./icons/wearable.png")
        
        patient >> mobile_app
        cgm_device >> mobile_app
        wearable >> mobile_app
    
    # 의료진 측 구성요소
    with Cluster("의료진 측"):
        doctor = User("의료진")
        web_dashboard = Custom("웹 대시보드", "./icons/dashboard.png")
        
        doctor >> web_dashboard
    
    # Cisco Webex 구성요소
    with Cluster("Cisco Webex"):
        webex_api = Custom("Webex API", "./icons/webex-api.png")
        webex_instant = Custom("Webex Instant\nConnect", "./icons/webex-instant.png")
        webex_meetings = Custom("Webex Meetings", "./icons/webex-meetings.png")
        webex_teams = Custom("Webex Teams", "./icons/webex-teams.png")
        
        webex_api >> webex_instant
        webex_api >> webex_meetings
        webex_api >> webex_teams
    
    # 백엔드 시스템
    with Cluster("백엔드 시스템"):
        # API 서버
        api_server = Flask("API 서버")
        
        # 데이터베이스
        db = PostgreSQL("환자 데이터\nDB")
        
        # 혈당 예측 엔진
        with Cluster("혈당 예측 엔진"):
            bit_maml = Custom("BiT-MAML 모델", "./icons/ml-model.png")
            prediction_service = Lambda("예측 서비스")
            
            bit_maml >> prediction_service
        
        # 알림 시스템
        notification = Custom("알림 시스템", "./icons/notification.png")
        
        # 데이터 분석
        analytics = BigQuery("데이터 분석")
        
        # 연결
        api_server >> db
        api_server >> prediction_service
        api_server >> notification
        db >> analytics
    
    # 연결 관계
    mobile_app >> internet >> api_server
    web_dashboard >> internet >> api_server
    api_server >> internet >> webex_api
    
    # 주요 데이터 흐름
    cgm_data_flow = Edge(label="혈당 데이터")
    prediction_flow = Edge(label="예측 결과", style="dashed")
    alert_flow = Edge(label="긴급 알림", color="red", style="bold")
    
    mobile_app >> cgm_data_flow >> api_server
    prediction_service >> prediction_flow >> api_server
    notification >> alert_flow >> webex_instant
    webex_instant >> doctor
    webex_instant >> patient
