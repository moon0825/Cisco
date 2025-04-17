# -*- coding: utf-8 -*-
from threading import Thread

import pytz
from flask import Flask, request, jsonify, send_from_directory  # send_from_directory 제거
from flask_restful import Api, Resource
from flask_cors import CORS
import os
import json
from apscheduler.schedulers.background import BackgroundScheduler
import time
from datetime import datetime, timedelta, timezone
import random
import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core import exceptions as google_exceptions
import numpy as np
from bit_maml import run_prediction_task
from bit_maml import predict_and_store_once

KST = pytz.timezone("Asia/Seoul")

# --- Firebase 초기화 ---
# GOOGLE_APPLICATION_CREDENTIALS 환경 변수 또는 기본 경로의 키 파일을 사용하여 초기화 시도
SERVICE_ACCOUNT_KEY_PATH = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "./ccccssss2-bde41-firebase-adminsdk-fbsvc-9438d30e40.json")
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


# 최근 12개 데이터를 가져오는 함수
def get_recent_glucose_features(patient_id, limit=12):
    logs_ref = db.collection("users").document("kimjaehoug").collection("glulog")
    logs = logs_ref.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(limit).stream()

    data = []
    for log in logs:
        entry = log.to_dict()
        if 'glucose' in entry and 'exercise' in entry and 'meal' in entry:
            data.append([
                float(entry["glucose"]),
                float(entry["exercise"]),
                float(entry["meal"])
            ])

    if len(data) < limit:
        raise ValueError("Not enough recent data (need at least 12)")

    return np.array(data[::-1])


# 예측하고 저장하는 함수


# 백그라운드 쓰레드



# 서버 시작 시 백그라운드 루프 시작
# --- Flask 앱 설정 ---
app = Flask(__name__,static_folder=None)
# CORS 설정: 프론트엔드 Vercel 주소를 명시적으로 허용하는 것이 좋음
FRONTEND_URL = os.environ.get("FRONTEND_URL","https://127.0.0.1:5371") # 기본값 설정
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
# 한국 시간대 (KST)
def firestore_timestamp_to_iso(timestamp):
    if timestamp is None:
        return None

    try:
        # Firestore Timestamp 객체 (datetime)
        if isinstance(timestamp, datetime):
            return timestamp.isoformat(timespec='seconds')

        # 문자열인 경우
        if isinstance(timestamp, str):
            try:
                dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
                return dt.isoformat(timespec='seconds')
            except ValueError:
                print(f"[경고] 문자열 timestamp 파싱 실패: {timestamp}")
                return None

    except Exception as e:
        print(f"[에러] timestamp 변환 실패: {timestamp} → {e}")
        return None

# --- API 리소스 정의 (Firestore 사용) ---

class PatientResource(Resource):
    def get(self, patient_id):
        if not db:
            return {"error": "Database service unavailable"}, 503
        try:
            patient_ref = db.collection('users').document(patient_id)
            doc = patient_ref.get()

            if doc.exists:
                data = doc.to_dict()
                # 직렬화 불가능한 타입 정제
                for key, value in data.items():
                    if isinstance(value, set):
                        data[key] = list(value)

                # 누락된 필드 보완
                data.setdefault("name", "이름 없음")
                data.setdefault("target_glucose_range", {"min": 70, "max": 180})
                return data, 200

            else:
                # 문서 없을 시 기본 환자 정보 리턴
                default_data = {
                    "name": "김재홍",
                    "target_glucose_range": {"min": 40, "max": 200}
                }
                return default_data, 200

        except Exception as e:
            print(f"[PatientResource.get] Error for patient_id={patient_id}: {e}")
            return {"error": "Internal server error fetching patient data"}, 500



class StateResource(Resource):
    def get(self, patient_id):
        """상태 기록 조회"""
        if not db:
            return {"error": "Database unavailable"}, 503
        try:
            docs = db.collection("state").document(patient_id).collection("log") \
                .order_by("time", direction=firestore.Query.DESCENDING) \
                .limit(50).stream()

            states = []
            for doc in docs:
                data = doc.to_dict()
                time_raw = data.get("time")
                # time 필드가 datetime일 경우 ISO 포맷으로 직렬화
                if isinstance(time_raw, datetime):
                    time_str = time_raw.astimezone(pytz.timezone("Asia/Seoul")).isoformat()
                else:
                    time_str = time_raw  # 혹시 모르니 fallback

                states.append({
                    "time": time_str,
                    "state": data.get("state"),
                    "meal": data.get("meal", 0),
                    "exercise": data.get("exercise", 0)
                })

            return {"states": states}, 200

        except Exception as e:
            print(f"[StateResource.get] Error: {e}")
            return {"error": "Internal server error fetching states"}, 500

    def post(self, patient_id):
        if not db:
            return {"error": "Database unavailable"}, 503
        try:
            payload = request.get_json()
            if not payload:
                return {"error": "No input data provided"}, 400

            # 어떤 상태인지 판별
            field_name = None
            value = None
            if "meal" in payload:
                field_name = "meal"
                value = float(payload["meal"])
                state_type = "meal"
            elif "exercise" in payload:
                field_name = "exercise"
                value = float(payload["exercise"])
                state_type = "exercise"
            else:
                return {"error": "Invalid state type. Only 'meal' or 'exercise' allowed."}, 400

            # --- 1단계: glulog 가장 최근 데이터 업데이트 ---
            logs_ref = db.collection("users").document(patient_id).collection("glulog")
            docs = logs_ref.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(1).stream()
            recent_doc = next(docs, None)

            if not recent_doc:
                return {"error": "No existing glucose log found to update"}, 404

            recent_doc_ref = logs_ref.document(recent_doc.id)
            recent_doc_ref.update({field_name: value})

            # --- 2단계: state/{patient_id}/{timestamp} 저장 ---
            now_kst = datetime.now(pytz.timezone("Asia/Seoul"))
            timestamp_str = now_kst.strftime("%Y-%m-%d %H:%M:%S")

            state_ref = db.collection("state").document(patient_id).collection("log").document(timestamp_str)
            state_ref.set({
                "state": state_type,
                "value": value,
                "time": timestamp_str,
                "patient_id": patient_id
            })

            print(f"[StateResource.post] ✅ {field_name}={value} 업데이트 완료 & 상태 기록 저장")
            predict_and_store_once(patient_id)
            return {"message": f"{field_name} updated & state saved"}, 200

        except Exception as e:
            print(f"[StateResource.post] Error: {e}")
            return {"error": "Internal server error updating state"}, 500
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
        if not db:
            return {"error": "Database service unavailable"}, 503
        try:
            # Firestore에서 해당 환자의 예측 데이터 조회
            pred_ref = db.collection("users").document(patient_id).collection("predict")
            docs = pred_ref.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(20).stream()

            predictions = []
            for doc in docs:
                data = doc.to_dict()
                predictions.append({
                    "timestamp": data.get("timestamp"),
                    "value": data.get("value"),
                    "predicted_at": data.get("predicted_at")
                })

            return {"predictions": predictions}, 200

        except Exception as e:
            print(f"[PredictionResource.get] Error for patient_id={patient_id}: {e}")
            return {"error": "Internal server error fetching predictions"}, 500


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
api.add_resource(StateResource, '/api/patients/<string:patient_id>/states')

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
    port = int(os.environ.get('PORT', 5371)) # 포트 번호 변경 가능성 고려 (기존 5371?)
    print(f"Starting server on port {port}...")

    Thread(target=run_prediction_task, daemon=True).start()
    # Vercel 배포 환경 감지하여 디버그 모드 결정
    is_vercel = os.environ.get('VERCEL') == '1'
    app.run(host='0.0.0.0', port=port, debug=not is_vercel)
