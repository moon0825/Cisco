# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify, send_from_directory  # send_from_directory 제거
from flask_restful import Api, Resource
from flask_cors import CORS
import os
import json
import time
from datetime import datetime, timedelta, timezone
import random
import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core import exceptions as google_exceptions

# --- Firebase 초기화 ---
# GOOGLE_APPLICATION_CREDENTIALS 환경 변수 또는 기본 경로의 키 파일을 사용하여 초기화 시도
SERVICE_ACCOUNT_KEY_PATH = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "./glucosecisco-firebase-adminsdk-fbsvc-a4317e66fb.json")
db = None
try:
    print(f"서비스 계정 키 파일 경로 확인: {SERVICE_ACCOUNT_KEY_PATH}")
    if not os.path.exists(SERVICE_ACCOUNT_KEY_PATH):
        raise FileNotFoundError(f"서비스 계정 키 파일이 존재하지 않습니다: {SERVICE_ACCOUNT_KEY_PATH}")
    if not os.access(SERVICE_ACCOUNT_KEY_PATH, os.R_OK):
         raise PermissionError(f"서비스 계정 키 파일 읽기 권한 없음: {SERVICE_ACCOUNT_KEY_PATH}")

    # 앱 중복 초기화 방지
    if not firebase_admin._apps:
        cred = credentials.Certificate(SERVICE_ACCOUNT_KEY_PATH)
        firebase_admin.initialize_app(cred)
        print("Firebase Admin SDK 초기화 성공")
    else:
        print("Firebase Admin SDK 이미 초기화됨")
    db = firestore.client()
    print("Firebase Firestore 클라이언트 생성 및 연결 성공")
    # 간단한 연결 테스트 (옵션)
    # db.collection('__test__').document('conn').set({'timestamp': firestore.SERVER_TIMESTAMP})
    # print("Firestore 쓰기 테스트 완료 (무시해도 됨)")
except Exception as e:
    print(f"!!! Firebase 초기화 중 심각한 오류 발생: {e} !!!")
    print("!!! Firestore 기능이 비활성화됩니다. 서비스 계정 키 경로 및 권한, 파일 형식을 확인하세요. !!!")
    db = None

# --- Flask 앱 설정 ---
app = Flask(__name__)
# CORS 설정: 프론트엔드 Vercel 주소를 명시적으로 허용하는 것이 좋음
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://cisco-git-main-kihoon-moons-projects.vercel.app") # 기본값 설정
CORS(app, origins=[FRONTEND_URL, "http://localhost:5000","*"], supports_credentials=True) # 로컬 및 배포 주소 허용
api = Api(app)

# --- Webex 통합 설정 (기존 코드 유지) ---
try:
    from webex_integration import WebexAPI, MedicalWebexIntegration
except ImportError:
    print("경고: webex_integration.py 모듈을 찾을 수 없습니다. Webex 기능이 시뮬레이션됩니다.")
    class WebexAPI: # Dummy
        def __init__(self, access_token=None): pass
        def get_user_info(self): return {"displayName": "Simulated User"}
    class MedicalWebexIntegration: # Dummy
         def __init__(self, webex_api): self.webex_api = webex_api
         def create_emergency_session(self, **kwargs): print("[SIM] 긴급 세션 생성:", kwargs); return {"id": "sim_session"}
         def send_glucose_alert(self, **kwargs): print("[SIM] 혈당 알림:", kwargs); return {"id": "sim_msg"}
         def schedule_regular_checkup(self, **kwargs): print("[SIM] 정기 검진 예약:", kwargs); return {"id": "sim_meeting"}

webex_api = None
medical_webex = None
webex_token = os.environ.get("WEBEX_ACCESS_TOKEN")

if not webex_token:
    print("Webex: ACCESS_TOKEN 없음. 시뮬레이션 모드.")
    webex_api_sim = WebexAPI()
    medical_webex = MedicalWebexIntegration(webex_api_sim)
else:
    try:
        webex_api = WebexAPI(access_token=webex_token)
        user_info = webex_api.get_user_info()
        print(f"Webex API 연결 성공: 사용자 '{user_info.get('displayName')}'")
        medical_webex = MedicalWebexIntegration(webex_api)
    except Exception as e:
        print(f"!!! Webex API 초기화 실패: {e} !!! 시뮬레이션 모드.")
        webex_api_sim = WebexAPI()
        medical_webex = MedicalWebexIntegration(webex_api_sim)

# --- Firestore 컬렉션 이름 상수화 ---
PATIENTS_COLLECTION = 'patients'
GLUCOSE_COLLECTION = 'glucoseReadings'
PREDICTIONS_COLLECTION = 'predictions'
ALERTS_COLLECTION = 'alerts'
# DOCTORS_COLLECTION = 'doctors' # 의사 정보 별도 관리 시

# --- Helper Function (기존 코드 유지) ---
def firestore_timestamp_to_iso(timestamp):
    if timestamp is None:
        return None
    if hasattr(timestamp, 'isoformat'):
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.isoformat(timespec='seconds')
    try:
        # 문자열을 datetime 객체로 파싱
        dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat(timespec='seconds')
    except Exception as e:
        print(f"[경고] timestamp 변환 실패: {timestamp} → {e}")
        return None

# --- API 리소스 정의 (Firestore 사용) ---

class PatientResource(Resource):
    """환자 정보 API"""
    def get(self, patient_id):
        return {"환자정보 API"}

class GlucoseResource(Resource):
    """혈당 데이터 API"""
    """혈당 데이터 API"""

    def get(self, patient_id):
        if not db:
            return {"error": "DB 미연결"}, 503
        try:
            hours = request.args.get('hours', default=24, type=int)
            limit = min(hours * 12, 1000)  # 최대 1000개 조회 (5분 간격 기준)

            # 하위 컬렉션 'glulog'에서 조회
            readings_query = db.collection('users') \
                .document(patient_id) \
                .collection('glulog') \
                .order_by('timestamp', direction=firestore.Query.DESCENDING) \
                .limit(limit)

            docs = readings_query.stream()
            readings_list = []
            for doc in docs:
                data = doc.to_dict()
                data['timestamp'] = firestore_timestamp_to_iso(data.get('timestamp'))
                # data['glucose'] = data.get('value', 0)  # 필요 시 사용
                readings_list.append(data)

            readings_list.reverse()  # 시간순 정렬 (오름차순)
            print(f"환자({patient_id}) 혈당 {len(readings_list)}개 조회 완료 (최대 {limit}개)")
            return {"readings": readings_list}, 200

        except google_exceptions.NotFound as e:
            print(f"Firestore 하위 컬렉션(glulog) 없음 오류: {e}")
            return {"error": f"DB 오류: 'glulog' 하위 컬렉션 없음"}, 500
        except google_exceptions.FailedPrecondition as e:
            if "index" in str(e).lower():
                print(f"!!! Firestore 인덱스 필요 오류: {e} !!!")
                return {"error": "DB 쿼리 인덱스 필요. Firestore 콘솔에서 생성하세요."}, 400
            else:
                print(f"Firestore 조건 오류 ({patient_id}): {e}")
                return {"error": "DB 조건 오류"}, 500
        except Exception as e:
            print(f"혈당({patient_id}) 조회 오류: {e}")
            return {"error": f"서버 오류 (혈당 조회): {str(e)}"}, 500

    def post(self, patient_id):
        return 500

    def _trigger_prediction_update(self, patient_id):
        return 500

    def _run_prediction_and_alerting_logic(self, patient_id):
        return 500

class PredictionResource(Resource):
    """예측 정보 API"""
    def get(self, patient_id):
        return 500


class AlertResource(Resource):
    """알림 정보 API"""
    def get(self, patient_id):
        return 500

    def put(self, patient_id, alert_id):
        return 500


# --- Webex 통합 API 엔드포인트 (Firestore 사용) ---
class WebexEmergencyConnect(Resource):
    def post(self):
        return 500

class WebexScheduleCheckup(Resource):
     def post(self):
         return 500


# --- 데모 데이터 생성 API (Firestore용) ---
class SeedDemoData(Resource):
     def post(self):
         return 500

# --- API 라우트 등록 ---
api.add_resource(PatientResource, '/api/patients/<string:patient_id>')
api.add_resource(GlucoseResource, '/api/patients/<string:patient_id>/glucose')
api.add_resource(PredictionResource, '/api/patients/<string:patient_id>/predictions')
api.add_resource(AlertResource, '/api/patients/<string:patient_id>/alerts', '/api/patients/<string:patient_id>/alerts/<string:alert_id>')
api.add_resource(WebexEmergencyConnect, '/api/webex/emergency_connect')
api.add_resource(WebexScheduleCheckup, '/api/webex/schedule_checkup')
api.add_resource(SeedDemoData, '/api/seed_demo_data')

# 서버 상태 확인 엔드포인트

# 프론트엔드 제공 라우트
@app.route('/')
def serve_index():
    index_path = 'index.html'
    static_folder = '../frontend'
    print(f"Serving {index_path} from {static_folder}")
    try:
        if not os.path.exists(os.path.join(static_folder, index_path)):
            raise FileNotFoundError(f"{index_path} 파일이 {static_folder} 폴더에 존재하지 않습니다.")
        return send_from_directory(static_folder, index_path)
    except FileNotFoundError as e:
        print(f"!!! 오류: {e} !!!")
        return jsonify({"error": f"Frontend file not found: {index_path} in {static_folder}"}), 404
    except Exception as e:
        print(f"!!! 오류: {e} !!!")
        return jsonify({"error": "Internal server error serving frontend"}), 500
@app.route('/static/<path:filename>')
def serve_static(filename):
    static_folder = '../frontend/static/'
    print(f"Serving static file {filename} from {static_folder}")
    try:
        if not os.path.exists(os.path.join(static_folder, filename)):
            raise FileNotFoundError(f"{filename} 파일이 {static_folder} 폴더에 존재하지 않습니다.")
        return send_from_directory(static_folder, filename)
    except FileNotFoundError as e:
        print(f"!!! 오류: {e} !!!")
        return jsonify({"error": f"Static file not found: {filename} in {static_folder}"}), 404
    except Exception as e:
        print(f"!!! 오류: {e} !!!")
        return jsonify({"error": "Internal server error serving static file"}), 500


# --- 메인 실행 ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000)) # 포트 번호 변경 가능성 고려 (기존 5371?)
    print(f"Starting server on port {port}...")
    # Vercel 배포 환경 감지하여 디버그 모드 결정
    is_vercel = os.environ.get('VERCEL') == '1'
    app.run(host='0.0.0.0', port=port, debug=not is_vercel)