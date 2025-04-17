# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify, redirect, session, url_for
from flask_restful import Api, Resource
from flask_cors import CORS
import os
import json
import time
from datetime import datetime, timedelta, timezone
import random
import requests # Webex OAuth 토큰 교환용
import uuid     # OAuth state 생성용
import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core import exceptions as google_exceptions
import base64 # Base64 디코딩용
import json   # JSON 파싱용

# --- Firebase 초기화 (Base64 방식) ---
SERVICE_ACCOUNT_KEY_BASE64 = os.environ.get('FIREBASE_SERVICE_ACCOUNT_BASE64')
db = None
try:
    if not SERVICE_ACCOUNT_KEY_BASE64:
        raise ValueError("FIREBASE_SERVICE_ACCOUNT_BASE64 환경 변수가 설정되지 않았습니다.")

    print("Base64 서비스 계정 키 디코딩 시도...")
    key_json_str = base64.b64decode(SERVICE_ACCOUNT_KEY_BASE64).decode('utf-8')
    key_dict = json.loads(key_json_str)
    print("서비스 계정 키 파싱 성공.")

    if not firebase_admin._apps:
        cred = credentials.Certificate(key_dict)
        firebase_admin.initialize_app(cred)
        print("Firebase Admin SDK 초기화 성공 (Base64)")
    else:
        print("Firebase Admin SDK 이미 초기화됨 (Base64)")
    db = firestore.client()
    print("Firebase Firestore 클라이언트 생성 및 연결 성공")
except Exception as e:
    print(f"!!! Firebase 초기화 중 심각한 오류 발생: {e} !!!")
    db = None

# --- Flask 앱 설정 ---
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'fallback-dev-secret-key-should-be-set')
if app.secret_key == 'fallback-dev-secret-key-should-be-set' and os.environ.get('VERCEL') == '1':
     print("!!! 경고: FLASK_SECRET_KEY 환경 변수가 설정되지 않았습니다. !!!")

FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://cisco-git-main-kihoon-moons-projects.vercel.app")
CORS(app, origins=[FRONTEND_URL, "http://localhost:5000"], supports_credentials=True)
api = Api(app)

# --- Webex OAuth 설정 ---
WEBEX_CLIENT_ID = os.environ.get('WEBEX_CLIENT_ID')
WEBEX_CLIENT_SECRET = os.environ.get('WEBEX_CLIENT_SECRET')
WEBEX_REDIRECT_URI = os.environ.get('WEBEX_REDIRECT_URI')
WEBEX_AUTHORIZE_URL = "https://webexapis.com/v1/authorize"
WEBEX_TOKEN_URL = "https://webexapis.com/v1/access_token"
WEBEX_SCOPES = os.environ.get("WEBEX_SCOPES", "spark:messages_write meeting:schedules_write meeting:schedules_read spark:people_read")

# --- Webex 통합 모듈 임포트 ---
try:
    from webex_integration import WebexAPI, MedicalWebexIntegration
except ImportError:
    print("경고: webex_integration.py 모듈을 찾을 수 없습니다. Webex 기능이 시뮬레이션됩니다.")
    class WebexAPI:
        def __init__(self, access_token=None): pass
        def get_user_info(self): return {"displayName": "Simulated User"}
    class MedicalWebexIntegration:
         def __init__(self, webex_api): self.webex_api = webex_api
         def create_emergency_session(self, **kwargs): print("[SIM] 긴급 세션 생성:", kwargs); return {"id": "sim_session", "joinUrl": "https://webex.example/sim"}
         def send_glucose_alert(self, **kwargs): print("[SIM] 혈당 알림:", kwargs); return {"id": "sim_msg"}
         def schedule_regular_checkup(self, **kwargs): print("[SIM] 정기 검진 예약:", kwargs); return {"id": "sim_meeting", "webLink": "https://webex.example/sim_meet"}

# --- Firestore 컬렉션 이름 ---
PATIENTS_COLLECTION = 'patients'
GLUCOSE_COLLECTION = 'glucoseReadings'
PREDICTIONS_COLLECTION = 'predictions'
ALERTS_COLLECTION = 'alerts'
TOKENS_COLLECTION = 'webex_tokens'
DOCTORS_COLLECTION = 'doctors' # 의사 정보용 (선택적)

# --- Helper Function ---
def firestore_timestamp_to_iso(timestamp):
    if timestamp and hasattr(timestamp, 'isoformat'):
        dt = timestamp # Assume it's already a datetime object from Firestore
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat(timespec='seconds')
    return None

# --- Webex Token 관리 함수 (Firestore 사용) ---
def store_tokens(user_id, access_token, refresh_token, expires_in):
    if not db: return False
    try:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in) - 300) # 5분 여유
        token_ref = db.collection(TOKENS_COLLECTION).document(user_id)
        token_ref.set({
            'user_id': user_id, 'access_token': access_token,
            'refresh_token': refresh_token, 'expires_at': expires_at
        }, merge=True)
        print(f"토큰 저장 완료: 사용자={user_id}")
        return True
    except Exception as e: print(f"!!! 토큰 저장 실패 ({user_id}): {e} !!!"); return False

def get_tokens(user_id):
    if not db: return None
    try:
        doc = db.collection(TOKENS_COLLECTION).document(user_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception as e: print(f"!!! 토큰 가져오기 실패 ({user_id}): {e} !!!"); return None

def refresh_tokens(user_id, refresh_token):
    if not db or not all([WEBEX_CLIENT_ID, WEBEX_CLIENT_SECRET]): return None
    if not refresh_token: print(f"!!! 토큰 갱신 불가: Refresh Token 없음 ({user_id}) !!!"); return None

    print(f"Webex 토큰 갱신 시도: 사용자={user_id}")
    payload = { 'grant_type': 'refresh_token', 'client_id': WEBEX_CLIENT_ID,
                'client_secret': WEBEX_CLIENT_SECRET, 'refresh_token': refresh_token }
    try:
        response = requests.post(WEBEX_TOKEN_URL, data=payload)
        response.raise_for_status()
        data = response.json()
        print(f"토큰 갱신 성공: 사용자={user_id}")
        store_tokens(user_id, data['access_token'], data.get('refresh_token', refresh_token), data['expires_in'])
        return data['access_token']
    except requests.exceptions.RequestException as e:
        print(f"!!! 토큰 갱신 API 요청 실패 ({user_id}): {e} !!!")
        if e.response is not None and e.response.status_code in [400, 401]:
            print("Refresh Token 만료/오류 가능성. 재인증 필요.")
            try: db.collection(TOKENS_COLLECTION).document(user_id).delete(); print(f"갱신 실패로 사용자 {user_id} 토큰 삭제됨")
            except: pass
        return None

def get_valid_webex_token(user_id):
    token_data = get_tokens(user_id)
    if not token_data: return None

    expires_at = token_data.get('expires_at')
    # Firestore Timestamp -> Python datetime 변환 (타임존 인식)
    if expires_at and hasattr(expires_at, 'replace') and not isinstance(expires_at, datetime):
         try: expires_at = expires_at.replace(tzinfo=timezone.utc)
         except: expires_at = None

    if isinstance(expires_at, datetime) and expires_at > datetime.now(timezone.utc):
        print(f"유효한 토큰 사용: 사용자={user_id}")
        return token_data.get('access_token')
    else:
        print(f"토큰 만료 또는 시간 정보 없음, 갱신 시도: 사용자={user_id}")
        return refresh_tokens(user_id, token_data.get('refresh_token'))

def get_webex_api_client_for_user(user_id):
     access_token = get_valid_webex_token(user_id)
     return WebexAPI(access_token=access_token) if access_token else None

# --- OAuth 인증 관련 API 엔드포인트 ---
@app.route('/api/webex/auth/initiate')
def webex_auth_initiate():
    user_id_to_auth = request.args.get('user_id', 'doctor1') # 인증 대상 사용자 ID
    if not all([WEBEX_CLIENT_ID, WEBEX_REDIRECT_URI, WEBEX_SCOPES]):
        return jsonify({"error": "Webex OAuth 미설정"}), 500
    state = str(uuid.uuid4())
    session['webex_oauth_state'] = state
    session['webex_auth_user_id'] = user_id_to_auth
    params = { 'response_type': 'code', 'client_id': WEBEX_CLIENT_ID,
               'redirect_uri': WEBEX_REDIRECT_URI, 'scope': WEBEX_SCOPES, 'state': state }
    auth_url = f"{WEBEX_AUTHORIZE_URL}?{requests.compat.urlencode(params)}"
    print(f"Webex 인증 시작: 사용자={user_id_to_auth}, URL={auth_url}")
    return redirect(auth_url)

@app.route('/api/webex/auth/callback')
def webex_auth_callback():
    error = request.args.get('error')
    if error: return jsonify({"error": "Webex OAuth Error", "details": error}), 400
    code = request.args.get('code')
    state = request.args.get('state')
    expected_state = session.pop('webex_oauth_state', None)
    user_id = session.pop('webex_auth_user_id', None)

    if not expected_state or state != expected_state: return jsonify({"error": "Invalid OAuth state"}), 400
    if not code or not user_id: return jsonify({"error": "Missing code or user context"}), 400
    if not all([WEBEX_CLIENT_ID, WEBEX_CLIENT_SECRET, WEBEX_REDIRECT_URI]):
         return jsonify({"error": "Webex OAuth 미설정"}), 500

    payload = { 'grant_type': 'authorization_code', 'client_id': WEBEX_CLIENT_ID,
                'client_secret': WEBEX_CLIENT_SECRET, 'code': code, 'redirect_uri': WEBEX_REDIRECT_URI }
    try:
        response = requests.post(WEBEX_TOKEN_URL, data=payload)
        response.raise_for_status()
        data = response.json()
        success = store_tokens(user_id, data['access_token'], data['refresh_token'], data['expires_in'])
        if success:
             # TODO: 성공 후 프론트엔드 리디렉션 또는 메시지 개선
             return jsonify({"message": f"Webex 인증 성공: 사용자={user_id}"})
        else: return jsonify({"error": "토큰 저장 실패"}), 500
    except requests.exceptions.RequestException as e:
        print(f"!!! 토큰 교환 실패: {e} !!!")
        error_details = str(e); error_code = 500
        if e.response is not None:
             error_code = e.response.status_code; error_details = e.response.text
        return jsonify({"error": "토큰 교환 실패", "details": error_details}), error_code
    except Exception as e:
         print(f"!!! 토큰 교환/저장 오류: {e} !!!")
         return jsonify({"error": "서버 내부 오류 (토큰 교환)"}), 500

# --- API 리소스 정의 (Firestore 사용) ---

class PatientResource(Resource):
    def get(self, patient_id):
        if not db: return {"error": "DB 미연결"}, 503
        try:
            doc_ref = db.collection(PATIENTS_COLLECTION).document(patient_id)
            doc = doc_ref.get()
            if doc.exists:
                return doc.to_dict(), 200
            else:
                return {"error": "환자 없음"}, 404
        except Exception as e:
            print(f"환자({patient_id}) 조회 오류: {e}")
            return {"error": "서버 오류 (환자 조회)"}, 500

class GlucoseResource(Resource):
    def get(self, patient_id):
        if not db: return {"error": "DB 미연결"}, 503
        try:
            hours = request.args.get('hours', default=24, type=int)
            limit = min(hours * 12, 1000) # 5분 간격 가정

            readings_query = db.collection(GLUCOSE_COLLECTION) \
                                .where('patientId', '==', patient_id) \
                                .order_by('timestamp', direction=firestore.Query.DESCENDING) \
                                .limit(limit)
            docs = readings_query.stream()
            readings_list = []
            for doc in docs:
                data = doc.to_dict()
                data['timestamp'] = firestore_timestamp_to_iso(data.get('timestamp'))
                readings_list.append(data)
            readings_list.reverse() # 시간순
            print(f"환자({patient_id}) 혈당 {len(readings_list)}개 조회 완료")
            return {"readings": readings_list}, 200
        except google_exceptions.FailedPrecondition as e:
             if "index" in str(e).lower():
                 print(f"!!! Firestore 인덱스 필요: {e} !!!")
                 return {"error": "DB 쿼리 인덱스 필요"}, 400
             else: raise e
        except Exception as e:
            print(f"혈당({patient_id}) 조회 오류: {e}")
            return {"error": "서버 오류 (혈당 조회)"}, 500

    def post(self, patient_id):
        if not db: return {"error": "DB 미연결"}, 503
        try:
            if not db.collection(PATIENTS_COLLECTION).document(patient_id).get().exists:
                return {"error": "환자 없음"}, 404
            data = request.get_json()
            if not data or 'value' not in data: return {"error": "'value' 필수"}, 400

            new_reading_data = {
                'patientId': patient_id, 'value': data['value'],
                'unit': data.get('unit', 'mg/dL'), 'source': data.get('source', 'Manual'),
                'timestamp': firestore.SERVER_TIMESTAMP
            }
            update_time, doc_ref = db.collection(GLUCOSE_COLLECTION).add(new_reading_data)
            print(f'혈당 추가: ID={doc_ref.id}, 환자={patient_id} at {update_time}')
            self._trigger_prediction_update(patient_id)
            return {"message": "혈당 추가 성공", "id": doc_ref.id}, 201
        except Exception as e:
            print(f"혈당({patient_id}) 추가 오류: {e}")
            return {"error": "서버 오류 (혈당 추가)"}, 500

    def _trigger_prediction_update(self, patient_id):
        print(f"예측/알림 업데이트 트리거: {patient_id}")
        try:
            self._run_prediction_and_alerting_logic(patient_id)
        except Exception as e:
            print(f"!!! 예측/알림 로직 실행 중 오류 ({patient_id}): {e} !!!")

    def _run_prediction_and_alerting_logic(self, patient_id):
        if not db: return
        print(f"Firestore 예측/알림 로직 시작: {patient_id}")
        pred_ref = db.collection(PREDICTIONS_COLLECTION).document(patient_id)
        alert_collection = db.collection(ALERTS_COLLECTION)
        patient_ref = db.collection(PATIENTS_COLLECTION).document(patient_id)
        try:
            readings_query = db.collection(GLUCOSE_COLLECTION) \
                                .where('patientId', '==', patient_id) \
                                .order_by('timestamp', direction=firestore.Query.DESCENDING).limit(12)
            docs = list(readings_query.stream())
            if len(docs) < 1: # 데이터 부족 처리
                print(f"예측 위한 혈당 데이터 부족 ({len(docs)}개)"); return

            docs.reverse()
            latest_reading_doc = docs[-1].to_dict(); latest_reading_value = latest_reading_doc.get('value')
            latest_reading_ts = latest_reading_doc.get('timestamp')
            if latest_reading_value is None or latest_reading_ts is None: return

            # 예측 시뮬레이션 (TODO: 실제 모델 연동)
            prediction_30min = max(40, min(300, latest_reading_value + random.randint(-20, 20)))
            prediction_60min = max(40, min(300, prediction_30min + random.randint(-15, 15)))
            print(f"  예측 결과: 30min={prediction_30min}, 60min={prediction_60min}")

            prediction_data = {
                "current": {"timestamp": latest_reading_ts, "value": latest_reading_value},
                "prediction_30min": {"timestamp": latest_reading_ts + timedelta(minutes=30), "value": prediction_30min},
                "prediction_60min": {"timestamp": latest_reading_ts + timedelta(minutes=60), "value": prediction_60min},
                "status": "success", "updated_at": firestore.SERVER_TIMESTAMP
            }
            pred_ref.set(prediction_data)
            print(f"  Firestore 예측 저장 완료")

            # 알림 생성/전송 로직
            patient_snap = patient_ref.get()
            if not patient_snap.exists: return
            patient_info = patient_snap.to_dict(); doctor_id = patient_info.get("doctor_id")
            if not doctor_id: return
            target_range = patient_info.get("target_glucose_range", {"min": 70, "max": 180})
            alert_type = None; predicted_value = None
            if prediction_30min < target_range["min"]: alert_type = "low"; predicted_value = prediction_30min
            elif prediction_30min > target_range["max"]: alert_type = "high"; predicted_value = prediction_30min

            if alert_type:
                ten_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=10)
                recent_alert_query = alert_collection.where('patientId', '==', patient_id) \
                                        .where('type', '==', alert_type) \
                                        .where('timestamp', '>=', ten_minutes_ago).limit(1)
                if len(list(recent_alert_query.stream())) > 0: print(f"  중복 알림 방지({alert_type})"); return

                alert_message = f"30분 후 {alert_type} 혈당({predicted_value}mg/dL) 예측. 현재: {latest_reading_value}mg/dL"
                new_alert_data = { "patientId": patient_id, "type": alert_type, "predicted_value": predicted_value,
                                   "current_value": latest_reading_value, "time_window": 30, "message": alert_message,
                                   "timestamp": firestore.SERVER_TIMESTAMP, "current_reading_timestamp": latest_reading_ts,
                                   "status": "active", "acknowledged": False }
                alert_ref = alert_collection.add(new_alert_data)[1]
                print(f"  Firestore 알림 저장: ID={alert_ref.id}")

                # Webex 전송 (담당 의사의 토큰 사용)
                webex_api_client = get_webex_api_client_for_user(doctor_id)
                if webex_api_client:
                    medical_webex_instance = MedicalWebexIntegration(webex_api_client)
                    doctor_snap = db.collection(PATIENTS_COLLECTION).document(doctor_id).get() # 임시
                    if doctor_snap.exists:
                        doctor_info = doctor_snap.to_dict()
                        recommendation = "..." # 내용 생략
                        medical_webex_instance.send_glucose_alert(
                             recipient_email=doctor_info.get("email"), recipient_name=doctor_info.get("name"),
                             patient_name=patient_info.get("name"), glucose_value=latest_reading_value,
                             prediction=predicted_value, alert_type=f"{alert_type}_risk", recommendation=f"...",
                             alert_details_url=f"/dummy/url" # 실제 URL 필요
                        )
                        print(f"  Webex 알림 전송 완료 (의사: {doctor_info.get('email')})")
                    else: print(f"  의사({doctor_id}) 정보 없음, Webex 전송 불가")
                else: print(f"  의사({doctor_id}) Webex 토큰 없음/만료, Webex 전송 불가")
            else: print(f"  정상 범위 예측, 알림 생성 안 함")
        except Exception as e:
            print(f"!!! _run_prediction_and_alerting_logic 오류 ({patient_id}): {e} !!!")
            pred_ref.set({'status': 'error', 'error_message': str(e), 'updated_at': firestore.SERVER_TIMESTAMP}, merge=True)


class PredictionResource(Resource):
    def get(self, patient_id):
        if not db: return {"error": "DB 미연결"}, 503
        try:
            doc = db.collection(PREDICTIONS_COLLECTION).document(patient_id).get()
            if doc.exists:
                 data = doc.to_dict()
                 for key in ['current', 'prediction_30min', 'prediction_60min']:
                     if key in data and data[key] and 'timestamp' in data[key]:
                         data[key]['timestamp'] = firestore_timestamp_to_iso(data[key].get('timestamp'))
                 data['updated_at'] = firestore_timestamp_to_iso(data.get('updated_at'))
                 return data, 200
            else:
                 return {"error": "아직 예측 정보 없음"}, 404
        except Exception as e:
            print(f"예측({patient_id}) 조회 오류: {e}")
            return {"error": "서버 오류 (예측 조회)"}, 500

class AlertResource(Resource):
    def get(self, patient_id):
        if not db: return {"error": "DB 미연결"}, 503
        try:
            active_only = request.args.get('active_only', default='true', type=str).lower() == 'true'
            limit = request.args.get('limit', default=10, type=int)
            alerts_query = db.collection(ALERTS_COLLECTION).where('patientId', '==', patient_id)
            if active_only: alerts_query = alerts_query.where('status', '==', 'active')
            alerts_query = alerts_query.order_by('timestamp', direction=firestore.Query.DESCENDING).limit(limit)
            docs = alerts_query.stream()
            alerts_list = []
            for doc in docs:
                data = doc.to_dict(); data['id'] = doc.id
                data['timestamp'] = firestore_timestamp_to_iso(data.get('timestamp'))
                data['current_reading_timestamp'] = firestore_timestamp_to_iso(data.get('current_reading_timestamp'))
                alerts_list.append(data)
            return {"alerts": alerts_list}, 200
        except google_exceptions.FailedPrecondition as e:
             if "index" in str(e).lower(): return {"error": "DB 쿼리 인덱스 필요"}, 400
             else: raise e
        except Exception as e:
            print(f"알림({patient_id}) 조회 오류: {e}")
            return {"error": "서버 오류 (알림 조회)"}, 500

    def put(self, patient_id, alert_id):
        if not db: return {"error": "DB 미연결"}, 503
        data = request.get_json()
        if not data or ('status' not in data and 'acknowledged' not in data):
            return {"error": "'status' 또는 'acknowledged' 필수"}, 400
        try:
            alert_ref = db.collection(ALERTS_COLLECTION).document(alert_id)
            doc_snap = alert_ref.get()
            if not doc_snap.exists: return {"error": "알림 없음"}, 404
            if doc_snap.to_dict().get('patientId') != patient_id: return {"error": "환자 권한 없음"}, 403

            update_data = {}
            if 'status' in data: update_data['status'] = data['status']
            if 'acknowledged' in data: update_data['acknowledged'] = data['acknowledged']

            if update_data:
                update_data['acknowledged_at'] = firestore.SERVER_TIMESTAMP
                alert_ref.update(update_data)
                print(f"알림 업데이트 완료 ({alert_id}): {update_data}")
                updated_doc = alert_ref.get().to_dict(); updated_doc['id'] = alert_id
                updated_doc['timestamp'] = firestore_timestamp_to_iso(updated_doc.get('timestamp'))
                updated_doc['acknowledged_at'] = firestore_timestamp_to_iso(updated_doc.get('acknowledged_at'))
                return {"message": "알림 업데이트 성공", "alert": updated_doc}, 200
            else: return {"message": "변경 사항 없음"}, 304
        except Exception as e:
             print(f"알림({alert_id}) 업데이트 오류: {e}")
             return {"error": "서버 오류 (알림 업데이트)"}, 500

class WebexEmergencyConnect(Resource):
    def post(self):
        if not db: return {"error": "DB 미연결"}, 503
        data = request.get_json();
        if not data or 'patient_id' not in data: return {"error": "patient_id 필수"}, 400
        patient_id = data.get('patient_id')
        requesting_user_id = data.get('requesting_user_id', 'doctor1') # 요청자 ID (의사)

        webex_api_client = get_webex_api_client_for_user(requesting_user_id)
        if not webex_api_client:
            auth_url = url_for('webex_auth_initiate', user_id=requesting_user_id, _external=True) if 'webex_auth_initiate' in app.view_functions else None
            return {"error": "Webex 인증 필요", "reauth_url": auth_url}, 401

        medical_webex_instance = MedicalWebexIntegration(webex_api_client)
        try:
            patient_snap = db.collection(PATIENTS_COLLECTION).document(patient_id).get()
            if not patient_snap.exists: return {"error": "환자 없음"}, 404
            patient_info = patient_snap.to_dict(); doctor_id = patient_info.get("doctor_id")
            if not doctor_id: return {"error": "담당 의사 미지정"}, 400
            doctor_snap = db.collection(PATIENTS_COLLECTION).document(doctor_id).get() # 임시
            if not doctor_snap.exists: return {"error": f"의사({doctor_id}) 없음"}, 404
            doctor_info = doctor_snap.to_dict()
            pred_snap = db.collection(PREDICTIONS_COLLECTION).document(patient_id).get()
            current_glucose = pred_snap.to_dict().get('current',{}).get('value','N/A') if pred_snap.exists else 'N/A'
            predicted_glucose = pred_snap.to_dict().get('prediction_30min',{}).get('value','N/A') if pred_snap.exists else 'N/A'

            print(f"Webex 긴급 연결 시도 (사용자 {requesting_user_id})...")
            session_info = medical_webex_instance.create_emergency_session(
                patient_email=patient_info.get("email"), patient_name=patient_info.get("name"),
                glucose_value=current_glucose, prediction=predicted_glucose,
                doctor_email=doctor_info.get("email") )
            print(f"Webex 긴급 연결 성공: 세션 ID={session_info.get('id')}")
            return session_info, 201
        except Exception as e:
            print(f"!!! Webex 긴급 연결 실패: {e} !!!")
            # TODO: Webex API 401 에러 처리 -> 토큰 갱신 실패 시 재인증 유도
            return {"error": f"Webex 긴급 연결 실패: {str(e)}"}, 500

class WebexScheduleCheckup(Resource):
    def post(self):
        if not db: return {"error": "DB 미연결"}, 503
        data = request.get_json()
        if not data or 'patient_id' not in data or 'start_time' not in data:
             return {"error": "'patient_id', 'start_time' 필수"}, 400
        patient_id = data.get('patient_id'); start_time_str = data.get('start_time')
        requesting_user_id = data.get('requesting_user_id', 'doctor1') # 요청자 ID

        webex_api_client = get_webex_api_client_for_user(requesting_user_id)
        if not webex_api_client:
            auth_url = url_for('webex_auth_initiate', user_id=requesting_user_id, _external=True) if 'webex_auth_initiate' in app.view_functions else None
            return {"error": "Webex 인증 필요", "reauth_url": auth_url}, 401

        medical_webex_instance = MedicalWebexIntegration(webex_api_client)
        try:
            datetime.fromisoformat(start_time_str.replace('Z', '+00:00')) # 시간 형식 검증
            # Firestore에서 환자/의사 정보 조회... (EmergencyConnect와 유사)
            patient_snap = db.collection(PATIENTS_COLLECTION).document(patient_id).get()
            if not patient_snap.exists: return {"error": "환자 없음"}, 404
            patient_info = patient_snap.to_dict(); doctor_id = patient_info.get("doctor_id")
            if not doctor_id: return {"error": "담당 의사 미지정"}, 400
            doctor_snap = db.collection(PATIENTS_COLLECTION).document(doctor_id).get() # 임시
            if not doctor_snap.exists: return {"error": f"의사({doctor_id}) 없음"}, 404
            doctor_info = doctor_snap.to_dict()

            print(f"Webex 정기 검진 예약 시도 (사용자 {requesting_user_id})...")
            meeting_info = medical_webex_instance.schedule_regular_checkup(
                patient_email=patient_info.get("email"), patient_name=patient_info.get("name"),
                doctor_email=doctor_info.get("email"), doctor_name=doctor_info.get("name"),
                start_time=start_time_str, duration_minutes=data.get('duration_minutes', 30),
                notes=data.get('notes') )
            print(f"Webex 정기 검진 예약 성공: 미팅 ID={meeting_info.get('id')}")
            return { "message": "미팅 예약 성공", "meeting_id": meeting_info.get('id'),
                     "join_url": meeting_info.get('webLink') }, 201
        except ValueError: return {"error": "잘못된 시간 형식 (ISO 8601 필요)"}, 400
        except Exception as e:
             print(f"!!! Webex 정기 검진 예약 실패: {e} !!!")
             return {"error": f"Webex 미팅 예약 실패: {str(e)}"}, 500

class SeedDemoData(Resource):
    def post(self):
        if not db: return {"error": "DB 미연결"}, 503
        print("*** 데모 데이터 Firestore 시딩 시작 ***")
        try:
            # 데모 환자/의사 ID 결정 (환경 변수 우선, 없으면 기본값)
            patient_demo_id = os.environ.get("DEMO_PATIENT_ID", "patient1")
            doctor_demo_id = os.environ.get("DEMO_DOCTOR_ID", "doctor1")

            patient_ref = db.collection(PATIENTS_COLLECTION).document(patient_demo_id)
            patient_data = {
                "name": "김민수(데모)", "age": 28, "type": "1형 당뇨", "diagnosis_date": "2024-01-01",
                "doctor_id": doctor_demo_id, "insulin_regimen": "MDI",
                "target_glucose_range": {"min": 70, "max": 180},
                "email": os.environ.get("TEST_PATIENT_EMAIL", f"{patient_demo_id}_demo@example.com")
            }
            patient_ref.set(patient_data)
            print(f"환자 '{patient_demo_id}' 데이터 생성/덮어쓰기 완료")

            doctor_ref = db.collection(PATIENTS_COLLECTION).document(doctor_demo_id) # 임시: patients 사용
            doctor_data = {
                "name": "이지원(데모)", "specialty": "내분비내과", "hospital": "데모병원",
                "email": os.environ.get("TEST_DOCTOR_EMAIL", f"{doctor_demo_id}_demo@example.com")
            }
            doctor_ref.set(doctor_data)
            print(f"의사 '{doctor_demo_id}' 데이터 생성/덮어쓰기 완료")

            # 샘플 혈당 데이터 (기존 데이터 삭제 후 생성 또는 추가)
            print(f"샘플 혈당 데이터 생성 시작 ({patient_demo_id})...")
            now = datetime.now(timezone.utc); batch = db.batch(); count = 0
            current_glucose = random.randint(90, 140) # 시작 혈당 랜덤화
            for i in range(24 * 6): # 최근 12시간 (10분 간격)
                 timestamp = now - timedelta(minutes=10 * i)
                 current_glucose = max(40, min(300, current_glucose + random.randint(-8, 8)))
                 reading_data = {'patientId': patient_demo_id, 'value': current_glucose, 'unit': 'mg/dL',
                                 'source': 'CGM_Seed', 'timestamp': timestamp }
                 doc_ref = db.collection(GLUCOSE_COLLECTION).document()
                 batch.set(doc_ref, reading_data); count += 1
            batch.commit()
            print(f"샘플 혈당 데이터 {count}개 생성 완료")

            # 초기 예측 트리거
            GlucoseResource()._trigger_prediction_update(patient_demo_id)

            return {"message": f"Demo data seeded successfully for patient '{patient_demo_id}' and doctor '{doctor_demo_id}'!"}, 201
        except Exception as e:
            print(f"!!! 데모 데이터 시딩 중 오류: {e} !!!")
            return {"error": f"Failed to seed demo data: {str(e)}"}, 500

# --- API 라우트 등록 ---
api.add_resource(PatientResource, '/api/patients/<string:patient_id>')
api.add_resource(GlucoseResource, '/api/patients/<string:patient_id>/glucose')
api.add_resource(PredictionResource, '/api/patients/<string:patient_id>/predictions')
api.add_resource(AlertResource, '/api/patients/<string:patient_id>/alerts', '/api/patients/<string:patient_id>/alerts/<string:alert_id>')
api.add_resource(WebexEmergencyConnect, '/api/webex/emergency_connect')
api.add_resource(WebexScheduleCheckup, '/api/webex/schedule_checkup')
api.add_resource(SeedDemoData, '/api/seed_demo_data')

# 서버 상태 확인 엔드포인트
@app.route('/api/status', methods=['GET'])
def status():
    webex_config_status = "Configured" if all([WEBEX_CLIENT_ID, WEBEX_CLIENT_SECRET, WEBEX_REDIRECT_URI]) else "Not Configured"
    db_status = "Connected" if db else "Disconnected"
    return jsonify({
        "status": "online", "version": "0.7.0-oauth-firestore", # 버전 업데이트
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "webex_config_status": webex_config_status,
        "database_status": db_status
    })

# --- 정적 파일 서빙 라우트 제거 ---

# --- 메인 실행 ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"Starting server on port {port}...")
    is_vercel = os.environ.get('VERCEL') == '1'
    # Vercel에서는 debug=False, 로컬에서는 True
    app.run(host='0.0.0.0', port=port, debug=not is_vercel)